module dut #(
    parameter M = 8,
    parameter N = 8,
    parameter K = 16,
    parameter LANES = 16,
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

    localparam integer A_LEN   = M * K;   // 128
    localparam integer B_LEN   = K * N;   // 128
    localparam integer OUT_LEN = M * N;   // 64
    localparam integer A_BEATS = (A_LEN + LANES - 1) / LANES; // 8
    localparam integer B_BEATS = (B_LEN + LANES - 1) / LANES; // 8

    localparam integer OUT_BITS = $clog2(OUT_LEN); // 6
    localparam integer N_BITS   = $clog2(N);       // 3
    localparam integer UNROLL   = 8;
    localparam integer CALC_CYCLES = K / UNROLL;   // 2

    localparam [1:0] S_LOAD = 2'd0,
                     S_CALC = 2'd1,
                     S_WAIT = 2'd2;

    reg [1:0] state;
    reg [3:0] a_count;
    reg [3:0] b_count;
    reg [OUT_BITS-1:0] out_idx;
    reg [3:0] calc_count;
    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] result;
    reg out_valid_r;

    // A stored as eight 16-byte rows (each input beat is one whole A row).
    reg [LANES*DATA_W-1:0] a_row [0:M-1];
    // B stored transposed as eight 16-byte columns.
    reg [LANES*DATA_W-1:0] b_col [0:N-1];

    // Shift registers holding the current A row and B column while reducing K.
    reg [LANES*DATA_W-1:0] a_sr;
    reg [LANES*DATA_W-1:0] b_sr;

    wire [N_BITS-1:0]          col;
    wire [OUT_BITS-N_BITS-1:0] row;
    assign row = out_idx[OUT_BITS-1:N_BITS];
    assign col = out_idx[N_BITS-1:0];

    wire [OUT_BITS-1:0]        next_out_idx;
    wire [OUT_BITS-N_BITS-1:0] next_row;
    wire [N_BITS-1:0]          next_col;
    assign next_out_idx = out_idx + 1'b1;
    assign next_row     = next_out_idx[OUT_BITS-1:N_BITS];
    assign next_col     = next_out_idx[N_BITS-1:0];

    wire signed [DATA_W-1:0] a_tap [0:UNROLL-1];
    wire signed [DATA_W-1:0] b_tap [0:UNROLL-1];
    wire signed [15:0]       prod  [0:UNROLL-1];
    wire signed [ACC_W-1:0]  prod_ext [0:UNROLL-1];

    genvar u;
    generate
        for (u = 0; u < UNROLL; u = u + 1) begin : gen_lane
            assign a_tap[u] = a_sr[u*DATA_W +: DATA_W];
            assign b_tap[u] = b_sr[u*DATA_W +: DATA_W];
            assign prod[u] = a_tap[u] * b_tap[u];
            assign prod_ext[u] = {{(ACC_W-16){prod[u][15]}}, prod[u]};
        end
    endgenerate

    reg signed [ACC_W-1:0] sum_products;
    integer s;
    always_comb begin
        sum_products = 0;
        for (s = 0; s < UNROLL; s = s + 1) begin
            sum_products = sum_products + prod_ext[s];
        end
    end

    wire signed [ACC_W-1:0] acc_next;
    assign acc_next = (calc_count == 0) ? sum_products : (acc + sum_products);

    wire [LANES*DATA_W-1:0] a_sr_shifted;
    wire [LANES*DATA_W-1:0] b_sr_shifted;
    assign a_sr_shifted = a_sr >> (UNROLL*DATA_W);
    assign b_sr_shifted = b_sr >> (UNROLL*DATA_W);

    assign in_a_flat_ready = (state == S_LOAD) && (a_count < A_BEATS);
    assign in_b_flat_ready = (state == S_LOAD) && (b_count < B_BEATS);
    assign out_valid       = out_valid_r;
    assign out_c           = result;

    integer i;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state       <= S_LOAD;
            a_count     <= 4'd0;
            b_count     <= 4'd0;
            out_idx     <= 0;
            calc_count  <= 0;
            acc         <= 0;
            result      <= 0;
            out_valid_r <= 1'b0;
            a_sr        <= 0;
            b_sr        <= 0;
        end else begin
            case (state)
                S_LOAD: begin
                    if (in_a_flat_valid && in_a_flat_ready) begin
                        a_row[a_count[2:0]] <= in_a_flat;
                        a_count <= a_count + 1;
                    end

                    if (in_b_flat_valid && in_b_flat_ready) begin
                        for (i = 0; i < N; i = i + 1) begin
                            b_col[i][(2*b_count)*DATA_W +: DATA_W] <= in_b_flat[i*DATA_W +: DATA_W];
                            b_col[i][(2*b_count+1)*DATA_W +: DATA_W] <= in_b_flat[(N+i)*DATA_W +: DATA_W];
                        end
                        b_count <= b_count + 1;
                    end

                    if (a_count == A_BEATS && b_count == B_BEATS) begin
                        state      <= S_CALC;
                        out_idx    <= 0;
                        calc_count <= 0;
                        acc        <= 0;
                        a_sr       <= a_row[row];
                        b_sr       <= b_col[col];
                    end
                end

                S_CALC: begin
                    if (calc_count == CALC_CYCLES-1) begin
                        result      <= acc_next;
                        out_valid_r <= 1'b1;
                        state       <= S_WAIT;
                        calc_count  <= 0;
                    end else begin
                        acc        <= acc_next;
                        calc_count <= calc_count + 1;
                        a_sr       <= a_sr_shifted;
                        b_sr       <= b_sr_shifted;
                    end
                end

                S_WAIT: begin
                    if (out_ready) begin
                        out_valid_r <= 1'b0;
                        if (out_idx == OUT_LEN-1) begin
                            state      <= S_LOAD;
                            a_count    <= 4'd0;
                            b_count    <= 4'd0;
                            out_idx    <= 0;
                            calc_count <= 0;
                            acc        <= 0;
                        end else begin
                            state      <= S_CALC;
                            out_idx    <= next_out_idx;
                            calc_count <= 0;
                            acc        <= 0;
                            a_sr       <= a_row[next_row];
                            b_sr       <= b_col[next_col];
                        end
                    end
                end

                default: begin
                    state <= S_LOAD;
                end
            endcase
        end
    end

endmodule
