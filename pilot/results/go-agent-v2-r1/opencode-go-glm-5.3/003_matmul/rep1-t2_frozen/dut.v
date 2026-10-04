module dut #(
    parameter M      = 8,
    parameter N      = 8,
    parameter K      = 16,
    parameter LANES  = 16,
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

    // ------------------------------------------------------------------
    // B is fully consumed by every output element, so it is stored:
    //   b_st : B in arrival (beat) order. Beat t occupies [t*128 +: 128].
    //          Beat t holds flat elements 16t..16t+15 = B[2t][0..7],B[2t+1][0..7].
    //          Hence B[k][j] = b_st[k*64 + j*8 +: 8].
    //
    // A rows are consumed strictly in order (row i during elements 8i..8i+7),
    // so A is streamed lazily with a current row plus one prefetched row.
    // ------------------------------------------------------------------
    reg [1023:0] b_st;
    reg [127:0]  a_cur;     // A row currently being multiplied
    reg [127:0]  a_nxt;     // prefetched next A row
    reg          nxt_v;

    localparam S_LOAD = 1'b0;
    localparam S_RUN  = 1'b1;

    reg               state;
    reg [3:0]         bc;      // B beats accepted this transaction (0..8)
    reg [5:0]         pidx;    // element index being computed, 0..63
    reg               out_v;
    reg signed [19:0] out_r;   // |C| <= 16*128*128 = 2^18 : 20 bits is exact

    wire a_fire = in_a_flat_valid && in_a_flat_ready;
    wire b_fire = in_b_flat_valid && in_b_flat_ready;

    // A: accept whenever the prefetch slot is free (and not in the last row,
    // where the next beat would belong to the following transaction).
    assign in_a_flat_ready = !nxt_v &&
                             (state == S_LOAD ||
                              (state == S_RUN && pidx[5:3] != 3'd7));
    assign in_b_flat_ready = (state == S_LOAD) && (bc < 4'd8);

    // ------------------------------------------------------------------
    // B storage write
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (b_fire) begin
            case (bc[2:0])
                3'd0: b_st[127:0]    <= in_b_flat;
                3'd1: b_st[255:128]  <= in_b_flat;
                3'd2: b_st[383:256]  <= in_b_flat;
                3'd3: b_st[511:384]  <= in_b_flat;
                3'd4: b_st[639:512]  <= in_b_flat;
                3'd5: b_st[767:640]  <= in_b_flat;
                3'd6: b_st[895:768]  <= in_b_flat;
                3'd7: b_st[1023:896] <= in_b_flat;
                default: b_st[127:0] <= in_b_flat;
            endcase
        end
    end

    // ------------------------------------------------------------------
    // Compute datapath: one C element (16-term dot product) per cycle
    // ------------------------------------------------------------------
    wire [2:0] cs_col = pidx[2:0];   // output column j

    function [7:0] bsel;
        input [63:0] h;
        input [2:0]  c;
        begin
            case (c)
                3'd0: bsel = h[7:0];
                3'd1: bsel = h[15:8];
                3'd2: bsel = h[23:16];
                3'd3: bsel = h[31:24];
                3'd4: bsel = h[39:32];
                3'd5: bsel = h[47:40];
                3'd6: bsel = h[55:48];
                3'd7: bsel = h[63:56];
                default: bsel = h[7:0];
            endcase
        end
    endfunction

    wire signed [7:0] aop0  = a_cur[7:0];
    wire signed [7:0] aop1  = a_cur[15:8];
    wire signed [7:0] aop2  = a_cur[23:16];
    wire signed [7:0] aop3  = a_cur[31:24];
    wire signed [7:0] aop4  = a_cur[39:32];
    wire signed [7:0] aop5  = a_cur[47:40];
    wire signed [7:0] aop6  = a_cur[55:48];
    wire signed [7:0] aop7  = a_cur[63:56];
    wire signed [7:0] aop8  = a_cur[71:64];
    wire signed [7:0] aop9  = a_cur[79:72];
    wire signed [7:0] aop10 = a_cur[87:80];
    wire signed [7:0] aop11 = a_cur[95:88];
    wire signed [7:0] aop12 = a_cur[103:96];
    wire signed [7:0] aop13 = a_cur[111:104];
    wire signed [7:0] aop14 = a_cur[119:112];
    wire signed [7:0] aop15 = a_cur[127:120];

    wire signed [7:0] bop0  = bsel(b_st[63:0],     cs_col);
    wire signed [7:0] bop1  = bsel(b_st[127:64],   cs_col);
    wire signed [7:0] bop2  = bsel(b_st[191:128],  cs_col);
    wire signed [7:0] bop3  = bsel(b_st[255:192],  cs_col);
    wire signed [7:0] bop4  = bsel(b_st[319:256],  cs_col);
    wire signed [7:0] bop5  = bsel(b_st[383:320],  cs_col);
    wire signed [7:0] bop6  = bsel(b_st[447:384],  cs_col);
    wire signed [7:0] bop7  = bsel(b_st[511:448],  cs_col);
    wire signed [7:0] bop8  = bsel(b_st[575:512],  cs_col);
    wire signed [7:0] bop9  = bsel(b_st[639:576],  cs_col);
    wire signed [7:0] bop10 = bsel(b_st[703:640],  cs_col);
    wire signed [7:0] bop11 = bsel(b_st[767:704],  cs_col);
    wire signed [7:0] bop12 = bsel(b_st[831:768],  cs_col);
    wire signed [7:0] bop13 = bsel(b_st[895:832],  cs_col);
    wire signed [7:0] bop14 = bsel(b_st[959:896],  cs_col);
    wire signed [7:0] bop15 = bsel(b_st[1023:960], cs_col);

    wire signed [19:0] dot = aop0  * bop0  +
                            aop1  * bop1  +
                            aop2  * bop2  +
                            aop3  * bop3  +
                            aop4  * bop4  +
                            aop5  * bop5  +
                            aop6  * bop6  +
                            aop7  * bop7  +
                            aop8  * bop8  +
                            aop9  * bop9  +
                            aop10 * bop10 +
                            aop11 * bop11 +
                            aop12 * bop12 +
                            aop13 * bop13 +
                            aop14 * bop14 +
                            aop15 * bop15;

    // ------------------------------------------------------------------
    // Control
    // ------------------------------------------------------------------
    // crossing from row r to row r+1 (last element of a row, not the last row)
    wire bnd  = (pidx[2:0] == 3'd7) && (pidx[5:3] != 3'd7);
    // produce the next output word when the output slot is free and, at a
    // row boundary, the next A row has been prefetched
    wire step = (state == S_RUN) && (!out_v || out_ready) && (!bnd || nxt_v);

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= S_LOAD;
            bc    <= 4'd0;
            pidx  <= 6'd0;
            out_v <= 1'b0;
            out_r <= 20'sd0;
            nxt_v <= 1'b0;
            a_cur <= 128'd0;
            a_nxt <= 128'd0;
        end else begin
            if (a_fire) begin
                a_nxt <= in_a_flat;
                nxt_v <= 1'b1;
            end
            if (b_fire)
                bc <= bc + 4'd1;

            if (state == S_LOAD) begin
                // drain any pending final word of the previous transaction
                if (out_v && out_ready)
                    out_v <= 1'b0;
                // B fully loaded and first A row available -> start computing
                if ((bc == 4'd8) && nxt_v) begin
                    state <= S_RUN;
                    pidx  <= 6'd0;
                    a_cur <= a_nxt;
                    nxt_v <= 1'b0;
                end
            end else begin
                if (!out_v || out_ready) begin
                    if (step) begin
                        out_r <= dot;
                        out_v <= 1'b1;
                        if (pidx == 6'd63) begin
                            // last element of this transaction computed
                            state <= S_LOAD;
                            bc    <= 4'd0;
                            nxt_v <= 1'b0;
                        end else if (bnd) begin
                            // move to next row with the prefetched A row
                            a_cur <= a_nxt;
                            nxt_v <= 1'b0;
                            pidx  <= pidx + 6'd1;
                        end else begin
                            pidx <= pidx + 6'd1;
                        end
                    end else begin
                        // output word consumed but next row not yet here:
                        // drop out_valid for a cycle (legal stream gap)
                        out_v <= 1'b0;
                    end
                end
            end
        end
    end

    assign out_valid = out_v;
    assign out_c = {{(ACC_W-20){out_r[19]}}, out_r};

endmodule
