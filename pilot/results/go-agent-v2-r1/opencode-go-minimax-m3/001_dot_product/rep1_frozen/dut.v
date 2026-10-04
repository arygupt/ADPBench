// Dot product: c = sum_i a[i] * b[i] over signed int8 inputs.
// Two back-to-back transactions, each delivering 256 elements on each stream.
// 32 lanes per beat, 8 beats per transaction.
// 8 multipliers process a beat pair in 4 cycles.

module dut #(
    parameter LEN    = 256,
    parameter LANES  = 32,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
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

    localparam BEATS  = LEN / LANES;          // 8 beats per transaction
    localparam BEAT_W = LANES * DATA_W;       // 256 bits per beat

    // ------------------------------------------------------------
    // State machine
    // ------------------------------------------------------------
    typedef enum logic [1:0] {
        S_ACCM,
        S_OUT,
        S_DONE
    } state_t;

    state_t state;
    reg [3:0] beat_cnt;    // 0..7
    reg [1:0] txn_cnt;     // 0..1
    reg [1:0] grp_idx;     // 0..3 (group within beat, 8 elements per group)

    // ------------------------------------------------------------
    // Per-beat buffers
    // ------------------------------------------------------------
    reg [BEAT_W-1:0] a_reg, b_reg;
    reg              a_have, b_have;

    wire in_a_can_accept = (state == S_ACCM) && !a_have;
    wire in_b_can_accept = (state == S_ACCM) && !b_have;

    assign in_a_flat_ready = in_a_can_accept;
    assign in_b_flat_ready = in_b_can_accept;

    wire a_we = in_a_flat_valid && in_a_can_accept;
    wire b_we = in_b_flat_valid && in_b_can_accept;

    wire can_compute  = a_have && b_have && (state == S_ACCM);
    wire is_last_beat = (beat_cnt == BEATS - 1);
    wire is_last_grp  = (grp_idx == 2'd3);

    // ------------------------------------------------------------
    // Compute: 8 signed multiplies per cycle
    // grp_idx takes values 0,1,2,3 -> lane_base 0,8,16,24
    // ------------------------------------------------------------
    wire [4:0] lane_base = {grp_idx, 3'b000};

    wire signed [DATA_W-1:0] a [0:7];
    wire signed [DATA_W-1:0] b [0:7];

    genvar gi;
    generate
        for (gi = 0; gi < 8; gi++) begin : g_mul
            assign a[gi] = $signed(a_reg[(lane_base + 5'(gi))*DATA_W +: DATA_W]);
            assign b[gi] = $signed(b_reg[(lane_base + 5'(gi))*DATA_W +: DATA_W]);
        end
    endgenerate

    wire signed [2*DATA_W-1:0] p [0:7];

    generate
        for (gi = 0; gi < 8; gi++) begin : g_prod
            assign p[gi] = a[gi] * b[gi];
        end
    endgenerate

    // 8 -> 4 -> 2 -> 1 tree
    wire signed [2*DATA_W+1-1:0] s1 [0:3];
    wire signed [2*DATA_W+2-1:0] s2 [0:1];
    wire signed [2*DATA_W+3-1:0] dot_full;

    generate
        for (gi = 0; gi < 4; gi++) assign s1[gi] = $signed(p[2*gi]) + $signed(p[2*gi+1]);
        for (gi = 0; gi < 2; gi++) assign s2[gi] = s1[2*gi] + s1[2*gi+1];
        assign dot_full = s2[0] + s2[1];
    endgenerate

    // ------------------------------------------------------------
    // Registers
    // ------------------------------------------------------------
    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] out_reg;
    reg                    out_valid_r;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state        <= S_ACCM;
            beat_cnt     <= 0;
            txn_cnt      <= 0;
            grp_idx      <= 0;
            a_have       <= 0;
            b_have       <= 0;
            acc          <= 0;
            out_reg      <= 0;
            out_valid_r  <= 0;
        end else begin
            case (state)
                S_ACCM: begin
                    if (a_we) begin
                        a_reg  <= in_a_flat;
                        a_have <= 1'b1;
                    end
                    if (b_we) begin
                        b_reg  <= in_b_flat;
                        b_have <= 1'b1;
                    end

                    if (can_compute) begin
                        if (is_last_grp) begin
                            if (is_last_beat) begin
                                out_reg     <= acc + $signed(dot_full);
                                out_valid_r <= 1'b1;
                                state       <= S_OUT;
                                acc         <= 0;
                                beat_cnt    <= 0;
                                grp_idx     <= 0;
                                a_have      <= 1'b0;
                                b_have      <= 1'b0;
                            end else begin
                                acc      <= acc + $signed(dot_full);
                                beat_cnt <= beat_cnt + 1'b1;
                                grp_idx  <= 0;
                                a_have   <= 1'b0;
                                b_have   <= 1'b0;
                            end
                        end else begin
                            acc     <= acc + $signed(dot_full);
                            grp_idx <= grp_idx + 1'b1;
                        end
                    end
                end

                S_OUT: begin
                    if (out_ready) begin
                        out_valid_r <= 1'b0;
                        if (txn_cnt == 2'd1) begin
                            state <= S_DONE;
                        end else begin
                            txn_cnt <= txn_cnt + 1'b1;
                            state   <= S_ACCM;
                        end
                    end
                end

                S_DONE: begin
                    // Hold
                end

                default: state <= S_ACCM;
            endcase
        end
    end

    assign out_c     = out_reg;
    assign out_valid = out_valid_r;

endmodule