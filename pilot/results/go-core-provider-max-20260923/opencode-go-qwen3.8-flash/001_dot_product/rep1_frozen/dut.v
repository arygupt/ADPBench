module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam integer BEATS = LEN / LANES;   // 8
    localparam integer CNTW  = $clog2(BEATS); // 3
    localparam integer NB    = (LEN + LANES - 1) / LANES; // number of parallel products
    localparam integer SUMW  = $clog2(NB+1) + DATA_W + 1; // per-beat sum width (int8*int8=16 bits * 8 lanes -> up to 19 bits)

    reg                    accepted;          // last transaction's output has been consumed
    reg [CNTW-1:0]         cnt;               // beat counter (0..BEATS-1)
    reg signed [ACC_W-1:0] acc;
    reg                    done;              // all beats summed for current transaction

    wire a_fire = in_a_flat_valid & in_a_flat_ready;
    wire b_fire = in_b_flat_valid & in_b_flat_ready;

    // ready: accept only when not yet finished this transaction
    assign in_a_flat_ready = (~done | accepted);
    assign in_b_flat_ready = (~done | accepted);

    assign out_valid = done & ~accepted;
    assign out_c     = acc;

    // ---- Parallel products for the incoming beat ----
    wire signed [DATA_W-1:0] ea [0:LANES-1];
    wire signed [DATA_W-1:0] eb [0:LANES-1];
    wire signed [2*DATA_W-1:0] pr [0:LANES-1];
    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_lane
            assign ea[gi] = in_a_flat[gi*DATA_W +: DATA_W];
            assign eb[gi] = in_b_flat[gi*DATA_W +: DATA_W];
            assign pr[gi] = ea[gi] * eb[gi];
        end
    endgenerate

    // ---- Balanced tree adder over the products (single stage per clock via registered partial sums is unnecessary; combinational tree is fine and fast) ----
    // We need a combinational sum of 32 int16 products -> fits in 17-bit signed. Compute in one go.
    function automatic signed [SUMW-1:0] tree_sum;
        input integer lo;
        input integer hi; // inclusive
        integer mid;
        begin
            if (lo == hi) tree_sum = {{(SUMW-(2*DATA_W)){1'b0}}, pr[lo][2*DATA_W-1], pr[lo]};
            else begin
                mid = (lo + hi) >> 1;
                tree_sum = tree_sum(lo, mid) + tree_sum(mid+1, hi);
            end
        end
    endfunction

    wire signed [SUMW-1:0] beat_sum = tree_sum(0, LANES-1);

    // extend to ACC_W
    wire signed [ACC_W-1:0] beat_ext = {{(ACC_W-SUMW){beat_sum[SUMW-1]}}, beat_sum};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            accepted <= 1'b0;
            cnt      <= {CNTW{1'b0}};
            acc      <= {ACC_W{1'b0}};
            done     <= 1'b0;
        end else begin
            // Output handshake
            if (out_valid && out_ready) begin
                accepted <= 1'b1;
                done     <= 1'b0;
                cnt      <= {CNTW{1'b0}};
                acc      <= {ACC_W{1'b0}};
            end

            // Input processing
            if (a_fire && b_fire) begin
                // both streams aligned on same beat count by construction
                if (cnt == BEATS-1) begin
                    acc  <= acc + beat_ext;
                    done <= 1'b1;
                    cnt  <= {CNTW{1'b0}};
                    accepted <= 1'b0; // will be set when output consumed; safe because done blocks re-entry until accepted
                end else begin
                    acc <= acc + beat_ext;
                    cnt <= cnt + 1'b1;
                end
            end
        end
    end

endmodule
